Added `scripts/extract_antigravity_proto_schema.py`, a developer tool that recovers
antigravity's (`agy`) protobuf schema by scanning the `agy` binary for its embedded
`FileDescriptorProto`s (antigravity ships no `.proto` files). It previously lived only as an
inline appendix in `libs/mngr_antigravity/dev/README.md`; promoting it to a committed script
lets the new antigravity schema-verification release test invoke it directly. Run it with
`uv run python scripts/extract_antigravity_proto_schema.py "$(which agy)" --grep CortexStep`
(use `-v` to debug-log the bounded set of descriptor candidates it skips).
