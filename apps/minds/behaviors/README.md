# Minds behavior corpus

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This corpus specifies the externally observable behavior of the minds *desktop client*.
Established minds terms are defined in the [workspace glossary](../docs/workspace/glossary.md) and are not redefined in this corpus; corpus-specific terms are defined in the README of the folder that specifies them.

## Corpus-wide conventions

The user-facing unit throughout is the *workspace* (an mngr host); an *agent* is a distinct mngr process running inside a workspace, and this corpus never uses *agent* to mean *workspace*.

## Out of scope for the whole corpus

- The Electron shell's own startup behavior -- deciding which window or splash to show at launch -- which runs before any page this corpus specifies is reached.
