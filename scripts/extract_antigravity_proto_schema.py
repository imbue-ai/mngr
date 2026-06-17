"""Recover antigravity's embedded protobuf schema from the ``agy`` binary.

NOTE -- best-effort black magic, NOT a style exemplar for the rest of the repo. This is a
heuristic scrape of an opaque, undocumented binary: it scans for byte patterns that *look like*
embedded ``FileDescriptorProto``s, walks them with hand-rolled varint parsing, and swallows the
(thousands of) expected mis-hits silently. It is inherently fragile to agy's build/layout and
recovers most -- not all -- descriptors (some legacy-gzip-registered ones are missed by design).
Run it as a dev/verification tool, not as a model for general repo conventions; the patterns
here (bare-ish parse guards, ``while True`` byte walks, broad probing) are specific to reverse-
engineering a hostile format. See ``libs/mngr_antigravity/regenerating_protobuf_schema.md`` and the companion decoder
(``libs/mngr_antigravity/imbue/mngr_antigravity/resources/decode_agy_transcript.py``).

Why this exists
---------------
antigravity (``agy``) stores each conversation as a SQLite ``.db`` (one file per conversation
under ``$HOME/.gemini/antigravity-cli/conversations/<id>.db``). The conversation rows live in
the ``steps`` table, and ``steps.step_payload`` is a **protobuf** blob (``gemini_coder.Step``)
-- not JSON. antigravity publishes no ``.proto`` schema (the GitHub repo is distribution-only)
and ships no export command, so to decode a conversation we need the field/enum layout.

The ``agy`` binary is built with ``google.golang.org/protobuf``, which embeds each ``.proto``
file's serialized ``FileDescriptorProto`` as a raw (uncompressed) byte slice. This script
scans the binary for those, validates them, and writes them out. From the recovered
descriptors you can read the real field names/numbers and enum values that
``libs/mngr_antigravity/imbue/mngr_antigravity/resources/decode_agy_transcript.py`` keys off.

antigravity ships ~weekly; releases are normally additive (which the number-keyed decoder
tolerates on its own), but agy controls both ends and could in principle reuse a field number
for a new meaning -- a change the decoder cannot detect by itself. So re-run this against each
new binary and diff the output (see ``libs/mngr_antigravity/regenerating_protobuf_schema.md`` for
the full procedure and the schema map). ``test_antigravity_proto_schema.py`` mechanizes that diff
as a release-marked test that runs this script (as a subprocess) against the installed ``agy``
binary.

Usage
-----
    uv run python scripts/extract_antigravity_proto_schema.py "$(which agy)" \
        --out /tmp/agy_descriptors [--grep Step] [-v]

``--grep`` prints message/enum types whose fully-qualified name contains the substring
(case-insensitive), e.g. ``--grep CortexStep``. Without ``--out`` nothing is written. ``-v``
turns on debug logging of the (bounded) descriptor-level skips.

Method
------
Each ``FileDescriptorProto`` begins with field 1 (``name``): tag ``0x0A``, a varint length,
then ``"<path>.proto"``. We anchor on those, walk only top-level fields valid for
``FileDescriptorProto`` (numbers 1..14) to find the extent, and accept the slice if it parses
and carries content. A handful of large descriptors that protobuf-go registers via the legacy
gzip path are not recovered this way (e.g. ``codeium_common.proto``); the targeted decoder does
not need them (see the decoder's module docstring).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable
from pathlib import Path

from google.protobuf import descriptor_pb2
from google.protobuf.message import DecodeError

# ty cannot see protobuf's dynamically generated descriptor classes (no stubs ship), though they
# exist at runtime. Bind them once here so the rest of the module needs no per-line suppressions.
_FileDescriptorProto = descriptor_pb2.FileDescriptorProto  # ty: ignore[unresolved-attribute]
_DescriptorProto = descriptor_pb2.DescriptorProto  # ty: ignore[unresolved-attribute]
_EnumDescriptorProto = descriptor_pb2.EnumDescriptorProto  # ty: ignore[unresolved-attribute]

_logger = logging.getLogger(__name__)

# Top-level field numbers defined by FileDescriptorProto (1..12 plus 13/14 for the
# edition-era additions). The extent walker stops at the first field outside this set.
_FDP_TOP_LEVEL_FIELDS = set(range(1, 15))


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while i < len(data):
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("varint truncated")


def _walk_extent(data: bytes, start: int) -> int | None:
    """Return the end offset of a plausible FileDescriptorProto at ``start``, or None.

    Walks only top-level fields whose number is valid for FileDescriptorProto and whose wire
    type is varint (0) or length-delimited (2), skipping over nested sub-messages by their
    length prefix. Stops at the first byte that is not such a field. The ``ValueError`` guards
    here are not errors: this is a heuristic probe of an opaque binary, so most candidate
    offsets hit truncated varints -- the expected "not a descriptor here" signal -- and are
    silently rejected (logging them would drown out the run; they number in the thousands).
    """
    i = start
    saw_name = False
    n = len(data)
    while i < n:
        try:
            tag, j = _read_varint(data, i)
        except ValueError:
            break
        field = tag >> 3
        wire = tag & 7
        if field not in _FDP_TOP_LEVEL_FIELDS or wire not in (0, 2):
            break
        i = j
        if wire == 0:
            try:
                _, i = _read_varint(data, i)
            except ValueError:
                return None
        else:
            try:
                length, k = _read_varint(data, i)
            except ValueError:
                break
            if k + length > n:
                break
            i = k + length
        if field == 1:
            saw_name = True
    return i if saw_name and i > start + 2 else None


def _find_anchors(data: bytes) -> list[int]:
    """Find offsets where a FileDescriptorProto's name field (``0x0A <len> "...proto"``) starts."""
    anchors: set[int] = set()
    needle = b".proto"
    pos = 0
    while True:
        p = data.find(needle, pos)
        if p < 0:
            break
        pos = p + 1
        end_name = p + len(needle)
        # The name bytes precede ``.proto``; the tag (0x0A) + length varint precede the name.
        for namelen in range(min(end_name, 120), 0, -1):
            s_name = end_name - namelen
            if s_name < 2:
                continue
            for vlen in (1, 2):
                t = s_name - 1 - vlen
                if t < 0 or data[t] != 0x0A:
                    continue
                try:
                    length, after = _read_varint(data, t + 1)
                except ValueError:
                    continue
                if length == namelen and after == s_name:
                    name = data[s_name:end_name]
                    if all(32 <= c < 127 for c in name):
                        anchors.add(t)
    return sorted(anchors)


def extract(binary_path: Path) -> dict[str, bytes]:
    """Return a mapping of ``.proto`` file name -> serialized FileDescriptorProto bytes."""
    data = binary_path.read_bytes()
    anchors = _find_anchors(data)
    best: dict[str, tuple[int, bytes]] = {}
    decode_failures = 0
    for anchor in anchors:
        end = _walk_extent(data, anchor)
        if end is None:
            continue
        blob = data[anchor:end]
        fdp = _FileDescriptorProto()
        try:
            fdp.ParseFromString(blob)
        except DecodeError:
            # A candidate slice that passed the cheap anchor heuristic but is not a valid
            # FileDescriptorProto. Bounded by the number of ``.proto`` anchors (hundreds), so
            # debug-logging it is affordable -- unlike the varint-walk rejections above.
            decode_failures += 1
            _logger.debug("anchor %d: slice is not a valid FileDescriptorProto", anchor)
            continue
        if not fdp.name.endswith(".proto"):
            _logger.debug("anchor %d: parsed name %r does not end in .proto", anchor, fdp.name)
            continue
        if not (fdp.message_type or fdp.enum_type or fdp.dependency):
            _logger.debug("anchor %d: descriptor %r has no message/enum/dependency content", anchor, fdp.name)
            continue
        # The extent walker can over- or under-shoot; keep the candidate whose re-serialized
        # size is closest to the slice (favours a clean, complete parse).
        score = abs(len(fdp.SerializeToString()) - len(blob))
        prev = best.get(fdp.name)
        if prev is None or score < prev[0]:
            best[fdp.name] = (score, blob)
    _logger.debug(
        "scanned %d anchors, recovered %d descriptors, %d decode failures",
        len(anchors),
        len(best),
        decode_failures,
    )
    return {name: blob for name, (_, blob) in best.items()}


def _print_file_matches(
    source: str,
    prefix: str,
    messages: Iterable[_DescriptorProto],
    enums: Iterable[_EnumDescriptorProto],
    lowered: str,
) -> None:
    """Print messages/enums under ``prefix`` whose FQN contains ``lowered``, recursing into nesting."""
    for enum in enums:
        fqn = f"{prefix}.{enum.name}"
        if lowered in fqn.lower():
            values = ", ".join(f"{v.number}={v.name}" for v in enum.value)
            print(f"ENUM {fqn} [{source}]: {values}")
    for message in messages:
        fqn = f"{prefix}.{message.name}"
        if lowered in fqn.lower():
            fields = ", ".join(f"f{f.number} {f.name}" for f in message.field)
            print(f"MSG  {fqn} [{source}]: {fields}")
        _print_file_matches(source, fqn, message.nested_type, message.enum_type, lowered)


def _print_matches(descriptors: dict[str, bytes], needle: str) -> None:
    """Print every message/enum whose fully-qualified name contains ``needle`` (case-insensitive)."""
    lowered = needle.lower()
    for fdp_bytes in descriptors.values():
        fdp = _FileDescriptorProto()
        fdp.ParseFromString(fdp_bytes)
        _print_file_matches(fdp.name, fdp.package, fdp.message_type, fdp.enum_type, lowered)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Path to the agy binary")
    parser.add_argument("--out", type=Path, default=None, help="Directory to write <name>.fdp files")
    parser.add_argument("--grep", default=None, help="Print message/enum FQNs containing this substring")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-log descriptor-level skips")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    descriptors = extract(args.binary)
    print(f"recovered {len(descriptors)} FileDescriptorProtos")

    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, blob in descriptors.items():
            (args.out / f"{name.replace('/', '__')}.fdp").write_bytes(blob)
        print(f"wrote descriptors to {args.out}")

    if args.grep is not None:
        _print_matches(descriptors, args.grep)


if __name__ == "__main__":
    main()
