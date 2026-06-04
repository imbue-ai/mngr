Fixed a `possibly-missing-submodule` type-checker warning in
`utils/cel_utils.py` by importing `MapType` directly from `celpy.celtypes`
instead of accessing it as `celpy.celtypes.MapType` (which relied on the
submodule being imported as a side effect). No behavior change.
