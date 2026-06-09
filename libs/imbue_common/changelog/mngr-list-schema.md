Tightened the "Literal with multiple options" ratchet regex so it no longer misfires on string literals.

- The `PREVENT_LITERAL_MULTIPLE_OPTIONS` pattern now has a negative lookbehind for a quote character, so code that *builds* the text `"Literal[...]"` (e.g. a type-annotation renderer) is no longer flagged. Real `Literal[a, b]` type annotations, which are never immediately preceded by a quote, are still caught.
