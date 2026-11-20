"""Release-marked checks that the transcript decoder's field map, turn_end_hook.sh's Stop fields and statusline.sh's payload fields still match agy.

``decode_agy_transcript.py`` walks agy's ``gemini_coder.Step`` protobuf by hard-coded field
numbers and enum values recovered from the binary's embedded descriptors (see
``libs/mngr_antigravity/regenerating_protobuf_schema.md``). agy ships ~weekly; additive changes
are tolerated automatically, but if a release ever reused one of those field numbers for a new
meaning the number-keyed walk would silently mis-decode. This test mechanizes step 1 of the README's
"Redoing this after an agy release" procedure: it runs ``scripts/extract_antigravity_proto_schema.py``
against the live binary and asserts every field number / enum value the decoder relies on still
matches -- turning the manual eyeball diff into an exact check.

It is marked ``release`` (not run in CI, run manually -- see CLAUDE.md) and **requires** the
``agy`` binary on PATH: there is nothing to verify against without it, so a missing binary is a
hard failure, not a skip. The extractor is run as a subprocess (rather than imported) because it
lives under the repo-root ``scripts/`` package, which is not on the path for a tests collected
under ``libs/``; this also exercises the exact command the README documents.

agy's statusLine payload is no protobuf message: agy marshals it from a Go struct, whose JSON keys
the statusLine test reads out of the type descriptors the Go compiler leaves in the binary.
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from google.protobuf import descriptor_pb2

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_antigravity.resources import decode_agy_transcript as dat
from imbue.mngr_antigravity.resources.testing import STATUSLINE_PAYLOAD_FIELD_PATHS
from imbue.mngr_antigravity.resources.testing import TURN_END_KIND_BY_TERMINATION_REASON
from imbue.mngr_antigravity.resources.testing import read_installed_agy_version

# ty cannot see protobuf's dynamically generated descriptor classes (no stubs ship), though they
# exist at runtime. Bind them once here so the walk below needs no per-line suppressions.
_FileDescriptorProto = descriptor_pb2.FileDescriptorProto  # ty: ignore[unresolved-attribute]
_DescriptorProto = descriptor_pb2.DescriptorProto  # ty: ignore[unresolved-attribute]
_EnumDescriptorProto = descriptor_pb2.EnumDescriptorProto  # ty: ignore[unresolved-attribute]

# libs/mngr_antigravity/imbue/mngr_antigravity/resources/<this file> -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_EXTRACTOR = _REPO_ROOT / "scripts" / "extract_antigravity_proto_schema.py"


def _load_descriptors(tmp_path: Path) -> dict[str, bytes]:
    """Run the extractor against the installed agy binary and read back the ``.fdp`` blobs."""
    agy_binary = shutil.which("agy")
    assert agy_binary is not None, (
        "agy must be installed on PATH to run this release-marked schema verification "
        "(there is nothing to verify the decoder against without it)"
    )
    assert _EXTRACTOR.is_file(), f"extractor script not found at {_EXTRACTOR}"
    out_dir = tmp_path / "descriptors"
    subprocess.run(
        [sys.executable, str(_EXTRACTOR), agy_binary, "--out", str(out_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return {path.name: path.read_bytes() for path in out_dir.glob("*.fdp")}


def _index(descriptors: dict[str, bytes]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Return ``(messages, enums)`` indexed by fully-qualified name.

    ``messages[fqn]`` maps field name -> field number; ``enums[fqn]`` maps value name -> value
    number. Nested messages/enums are included under their dotted FQN.
    """
    messages: dict[str, dict[str, int]] = {}
    enums: dict[str, dict[str, int]] = {}

    def _walk(
        prefix: str, message_types: Iterable[_DescriptorProto], enum_types: Iterable[_EnumDescriptorProto]
    ) -> None:
        for enum in enum_types:
            enums[f"{prefix}.{enum.name}"] = {value.name: value.number for value in enum.value}
        for message in message_types:
            fqn = f"{prefix}.{message.name}"
            messages[fqn] = {field.name: field.number for field in message.field}
            _walk(fqn, message.nested_type, message.enum_type)

    for blob in descriptors.values():
        fdp = _FileDescriptorProto()
        fdp.ParseFromString(blob)
        _walk(fdp.package, fdp.message_type, fdp.enum_type)
    return messages, enums


@pytest.mark.release
@pytest.mark.harness_internals(harness="antigravity")
def test_decoder_field_map_matches_installed_antigravity_binary(tmp_path: Path) -> None:
    """Every field number / enum value decode_agy_transcript.py hard-codes must match live agy."""
    messages, enums = _index(_load_descriptors(tmp_path))

    # gemini_coder.Step: the top-level step message the decoder walks.
    step = messages["gemini_coder.Step"]
    assert step["type"] == dat._STEP_TYPE
    assert step["status"] == dat._STEP_STATUS
    assert step["metadata"] == dat._STEP_METADATA
    assert step["code_action"] == dat._STEP_CODE_ACTION
    assert step["user_input"] == dat._STEP_USER_INPUT
    assert step["planner_response"] == dat._STEP_PLANNER_RESPONSE
    assert step["error_message"] == dat._STEP_ERROR_MESSAGE

    # CortexStepMetadata: created_at (a Timestamp) and source.
    metadata = messages["exa.cortex_pb.CortexStepMetadata"]
    assert metadata["created_at"] == dat._METADATA_CREATED_AT
    assert metadata["source"] == dat._METADATA_SOURCE

    # The content sub-messages the decoder reads text out of.
    user_input = messages["exa.cortex_pb.CortexStepUserInput"]
    assert user_input["query"] == dat._USER_INPUT_QUERY
    assert user_input["user_response"] == dat._USER_INPUT_RESPONSE

    planner = messages["exa.cortex_pb.CortexStepPlannerResponse"]
    assert planner["response"] == dat._PLANNER_RESPONSE_TEXT
    assert planner["thinking"] == dat._PLANNER_THINKING
    assert planner["tool_calls"] == dat._PLANNER_TOOL_CALLS

    tool_call = messages["exa.codeium_common_pb.ChatToolCall"]
    assert tool_call["name"] == dat._TOOL_CALL_NAME
    assert tool_call["arguments_json"] == dat._TOOL_CALL_ARGS

    # ERROR_MESSAGE text is nested: CortexStepErrorMessage.error (f3) -> CortexErrorDetails.
    error_message = messages["exa.cortex_pb.CortexStepErrorMessage"]
    assert error_message["error"] == dat._ERROR_MESSAGE_DETAILS
    error_details = messages["exa.cortex_pb.CortexErrorDetails"]
    assert error_details["user_error_message"] == dat._ERROR_DETAILS_USER_MESSAGE
    assert error_details["short_error"] == dat._ERROR_DETAILS_SHORT_ERROR
    assert error_details["full_error"] == dat._ERROR_DETAILS_FULL_ERROR

    # Enums: every value name the decoder maps must still carry the number it expects. The
    # recovered names are prefixed (e.g. CORTEX_STEP_TYPE_USER_INPUT); the decoder stores the
    # unprefixed tail.
    step_type = enums["exa.cortex_pb.CortexStepType"]
    for number, name in dat._STEP_TYPE_NAMES.items():
        assert step_type[f"CORTEX_STEP_TYPE_{name}"] == number

    step_source = enums["exa.cortex_pb.CortexStepSource"]
    for number, name in dat._STEP_SOURCE_NAMES.items():
        assert step_source[f"CORTEX_STEP_SOURCE_{name}"] == number

    step_status = enums["exa.cortex_pb.CortexStepStatus"]
    for number, name in dat._STEP_STATUS_NAMES.items():
        assert step_status[f"CORTEX_STEP_STATUS_{name}"] == number


@pytest.mark.release
@pytest.mark.harness_internals(harness="antigravity")
def test_turn_end_hook_fields_and_termination_reasons_match_installed_antigravity_binary(tmp_path: Path) -> None:
    """Every Stop field turn_end_hook.sh reads must exist in live agy, and it must map every termination reason agy defines."""
    messages, enums = _index(_load_descriptors(tmp_path))

    assert {"conversation_id", "last_user_input"} <= set(messages["exa.hooks_pb.HookArgsCommon"])
    assert "termination_reason" in messages["exa.hooks_pb.StopHookArgs"]
    reasons = {
        name.removeprefix("EXECUTOR_TERMINATION_REASON_") for name in enums["exa.cortex_pb.ExecutorTerminationReason"]
    }
    assert set(TURN_END_KIND_BY_TERMINATION_REASON) == reasons - {"UNSPECIFIED"}


_ELF_MAGIC: Final[bytes] = b"\x7fELF"
_ELF_LOAD_SEGMENT: Final[int] = 1
_ELF_DYNAMIC_SEGMENT: Final[int] = 2
_ELF_WRITABLE_FLAG: Final[int] = 2
_ELF_RELOCATION_TABLE_TAG: Final[int] = 7
_ELF_RELOCATION_TABLE_SIZE_TAG: Final[int] = 8
# R_X86_64_RELATIVE and R_AARCH64_RELATIVE: the slot holds the image base plus the addend.
_ELF_RELATIVE_RELOCATION_TYPES: Final[frozenset[int]] = frozenset({8, 1027})
_ELF_RELOCATION_TYPE_MASK: Final[int] = 0xFFFFFFFF
_MACHO_MAGIC: Final[bytes] = b"\xcf\xfa\xed\xfe"
_MACHO_HEADER_SIZE: Final[int] = 32
_MACHO_SEGMENT_COMMAND: Final[int] = 0x19
_MACHO_CHAINED_FIXUPS_COMMAND: Final[int] = 0x80000034
_MACHO_WRITABLE_PROTECTION: Final[int] = 2

# Layout of the Go runtime's type descriptors (internal/abi) on a 64-bit target.
_GO_KIND_OFFSET: Final[int] = 23
_GO_KIND_MASK: Final[int] = 0x1F
_GO_POINTER_KIND: Final[int] = 22
_GO_SLICE_KIND: Final[int] = 23
_GO_STRUCT_KIND: Final[int] = 25
_GO_ELEMENT_TYPE_OFFSET: Final[int] = 48
_GO_STRUCT_FIELDS_OFFSET: Final[int] = 56
_GO_STRUCT_TYPE_SIZE: Final[int] = 80
_GO_UNCOMMON_TYPE_SIZE: Final[int] = 16
_GO_STRUCT_FIELD_SIZE: Final[int] = 24
_GO_NAME_HAS_TAG_FLAG: Final[int] = 2
# How far before a tag's `json:"..."` its name record can start.
_GO_NAME_RECORD_SEARCH_WINDOW: Final[int] = 512
_MAX_GO_STRUCT_FIELD_COUNT: Final[int] = 4096


class _Segment(FrozenModel):
    """A range of an executable file and the address it loads at."""

    address: int
    file_offset: int
    size: int
    is_writable: bool


class _GoImage(FrozenModel):
    """A Go executable's bytes, with each relocated pointer written into its slot, and its load segments."""

    data: bytes
    segments: tuple[_Segment, ...]


def _find_file_offset(segments: Sequence[_Segment], address: int) -> int | None:
    for segment in segments:
        if segment.address <= address < segment.address + segment.size:
            return address - segment.address + segment.file_offset
    return None


def _load_elf_image(data: bytearray) -> _GoImage:
    """Read an ELF executable's load segments and write each relative relocation's target into its slot."""
    (program_header_offset,) = struct.unpack_from("<Q", data, 32)
    program_header_size, program_header_count = struct.unpack_from("<HH", data, 54)
    segments: list[_Segment] = []
    dynamic_tags: dict[int, int] = {}
    for index in range(program_header_count):
        segment_type, flags, file_offset, address, _, size = struct.unpack_from(
            "<IIQQQQ", data, program_header_offset + index * program_header_size
        )
        if segment_type == _ELF_LOAD_SEGMENT:
            segments.append(
                _Segment(
                    address=address, file_offset=file_offset, size=size, is_writable=bool(flags & _ELF_WRITABLE_FLAG)
                )
            )
        if segment_type == _ELF_DYNAMIC_SEGMENT:
            dynamic_tags = dict(struct.iter_unpack("<QQ", bytes(data[file_offset : file_offset + size])))
    relocations = b""
    if _ELF_RELOCATION_TABLE_TAG in dynamic_tags:
        table_offset = _find_file_offset(segments, dynamic_tags[_ELF_RELOCATION_TABLE_TAG])
        assert table_offset is not None, "the agy binary's relocation table lies outside its load segments"
        relocations = bytes(data[table_offset : table_offset + dynamic_tags[_ELF_RELOCATION_TABLE_SIZE_TAG]])
    for slot, info, addend in struct.iter_unpack("<QQq", relocations):
        slot_offset = _find_file_offset(segments, slot)
        if info & _ELF_RELOCATION_TYPE_MASK in _ELF_RELATIVE_RELOCATION_TYPES and slot_offset is not None:
            struct.pack_into("<q", data, slot_offset, addend)
    return _GoImage(data=bytes(data), segments=tuple(segments))


def _load_macho_image(data: bytearray) -> _GoImage:
    """Read a Mach-O executable's segments; its rebased pointers already sit in their slots as addresses."""
    (command_count,) = struct.unpack_from("<I", data, 16)
    segments: list[_Segment] = []
    command_offset = _MACHO_HEADER_SIZE
    for _ in range(command_count):
        command, command_size = struct.unpack_from("<II", data, command_offset)
        assert command != _MACHO_CHAINED_FIXUPS_COMMAND, (
            "the agy binary now encodes its pointers as chained fixups, which this reader does not decode"
        )
        if command == _MACHO_SEGMENT_COMMAND:
            address, _, file_offset, size, _, protection = struct.unpack_from("<QQQQii", data, command_offset + 24)
            segments.append(
                _Segment(
                    address=address,
                    file_offset=file_offset,
                    size=size,
                    is_writable=bool(protection & _MACHO_WRITABLE_PROTECTION),
                )
            )
        command_offset += command_size
    return _GoImage(data=bytes(data), segments=tuple(segments))


def _load_go_image(binary: Path) -> _GoImage:
    data = bytearray(binary.read_bytes())
    if data.startswith(_ELF_MAGIC):
        return _load_elf_image(data)
    assert data.startswith(_MACHO_MAGIC), f"{binary} is neither a 64-bit ELF nor a 64-bit Mach-O executable"
    return _load_macho_image(data)


def _read_pointer(image: _GoImage, address: int) -> int:
    """The 8-byte value in the slot at ``address``, or 0 for an address no segment loads."""
    file_offset = _find_file_offset(image.segments, address)
    if file_offset is None:
        return 0
    return struct.unpack_from("<Q", image.data, file_offset)[0]


def _read_go_kind(image: _GoImage, type_address: int) -> int:
    file_offset = _find_file_offset(image.segments, type_address)
    if file_offset is None:
        return 0
    return image.data[file_offset + _GO_KIND_OFFSET] & _GO_KIND_MASK


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """The unsigned varint at ``offset`` and the offset just past it; it ends where ``data`` does."""
    value = 0
    for shift in (0, 7, 14, 21):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
    return value, offset


def _read_go_name(data: bytes, offset: int) -> tuple[str, str, int]:
    """The name, the tag and the end offset of the Go name record at ``offset``, e.g. ``("TaskCount", 'json:"task_count,omitempty"', end)``."""
    name_length, name_offset = _read_varint(data, offset + 1)
    end_offset = name_offset + name_length
    tag = b""
    if data[offset] & _GO_NAME_HAS_TAG_FLAG:
        tag_length, tag_offset = _read_varint(data, end_offset)
        end_offset = tag_offset + tag_length
        tag = data[tag_offset:end_offset]
    return (
        data[name_offset : name_offset + name_length].decode(errors="replace"),
        tag.decode(errors="replace"),
        end_offset,
    )


def _read_json_key(tag: str) -> str | None:
    """The key ``encoding/json`` gives a field with this struct tag, when the tag names one."""
    match = re.search(r'(?:^| )json:"([^",]+)', tag)
    return match.group(1) if match is not None and match.group(1) != "-" else None


def _find_field_name_addresses(image: _GoImage, json_key: str) -> list[int]:
    """The address of every Go struct field name record whose tag gives the field ``json_key``."""
    pattern = re.compile(rb'json:"' + re.escape(json_key.encode()) + rb'[",]')
    addresses: list[int] = []
    for segment in image.segments:
        for match in pattern.finditer(image.data, segment.file_offset, segment.file_offset + segment.size):
            first_offset = max(match.start() - _GO_NAME_RECORD_SEARCH_WINDOW, segment.file_offset)
            for offset in range(first_offset, match.start()):
                name, tag, end_offset = _read_go_name(image.data, offset)
                if name.isidentifier() and end_offset > match.start() and _read_json_key(tag) == json_key:
                    addresses.append(offset - segment.file_offset + segment.address)
    return addresses


def _find_slots_pointing_at(image: _GoImage, addresses: Sequence[int]) -> list[int]:
    """The address of every slot in a writable segment that holds one of ``addresses``."""
    if not addresses:
        return []
    pattern = re.compile(b"|".join(re.escape(struct.pack("<Q", address)) for address in addresses))
    return [
        match.start() - segment.file_offset + segment.address
        for segment in image.segments
        if segment.is_writable
        for match in pattern.finditer(image.data, segment.file_offset, segment.file_offset + segment.size)
    ]


def _find_struct_holding_field(image: _GoImage, field_address: int) -> int | None:
    """The address of the struct type whose field array holds the entry at ``field_address``.

    The Go compiler lays a struct type out as its descriptor, then an uncommon type when the type is
    named, then its field array.
    """
    for field_index in range(_MAX_GO_STRUCT_FIELD_COUNT):
        first_field = field_address - field_index * _GO_STRUCT_FIELD_SIZE
        for uncommon_type_size in (_GO_UNCOMMON_TYPE_SIZE, 0):
            struct_address = first_field - uncommon_type_size - _GO_STRUCT_TYPE_SIZE
            if (
                _read_pointer(image, struct_address + _GO_STRUCT_FIELDS_OFFSET) == first_field
                and _read_go_kind(image, struct_address) == _GO_STRUCT_KIND
                and _read_pointer(image, struct_address + _GO_STRUCT_FIELDS_OFFSET + 8) > field_index
            ):
                return struct_address
    return None


def _read_field_types_by_json_key(image: _GoImage, struct_address: int) -> dict[str, int]:
    """The type address of each field of the struct type at ``struct_address`` that has a JSON key."""
    first_field = _read_pointer(image, struct_address + _GO_STRUCT_FIELDS_OFFSET)
    field_count = _read_pointer(image, struct_address + _GO_STRUCT_FIELDS_OFFSET + 8)
    field_types: dict[str, int] = {}
    for field_address in range(first_field, first_field + field_count * _GO_STRUCT_FIELD_SIZE, _GO_STRUCT_FIELD_SIZE):
        name_offset = _find_file_offset(image.segments, _read_pointer(image, field_address))
        json_key = _read_json_key(_read_go_name(image.data, name_offset)[1]) if name_offset is not None else None
        if json_key is not None:
            field_types[json_key] = _read_pointer(image, field_address + 8)
    return field_types


def _find_struct_reached_from(image: _GoImage, type_address: int) -> int | None:
    """The struct type that the type at ``type_address`` is, points to or is a slice of."""
    kind = _read_go_kind(image, type_address)
    while kind in (_GO_POINTER_KIND, _GO_SLICE_KIND):
        type_address = _read_pointer(image, type_address + _GO_ELEMENT_TYPE_OFFSET)
        kind = _read_go_kind(image, type_address)
    return type_address if kind == _GO_STRUCT_KIND else None


def _find_missing_json_paths(image: _GoImage, struct_address: int) -> dict[str, list[str]]:
    """Each statusLine payload path statusline.sh reads that the struct type at ``struct_address`` does not marshal.

    A path is cut at its first missing key and mapped to the JSON keys the struct has there.
    """
    json_keys_by_missing_path: dict[str, list[str]] = {}
    for json_path in STATUSLINE_PAYLOAD_FIELD_PATHS:
        parent_address: int | None = struct_address
        for depth, json_key in enumerate(json_path, start=1):
            field_types = _read_field_types_by_json_key(image, parent_address) if parent_address is not None else {}
            if json_key not in field_types:
                json_keys_by_missing_path[".".join(json_path[:depth])] = sorted(field_types)
                break
            parent_address = _find_struct_reached_from(image, field_types[json_key])
    return json_keys_by_missing_path


@pytest.mark.release
@pytest.mark.harness_internals(harness="antigravity")
def test_statusline_payload_fields_are_json_keys_of_one_go_struct_in_installed_antigravity_binary() -> None:
    """Every statusLine payload field statusline.sh reads must be a JSON key of one Go struct in live agy, nested as the script reads it."""
    agy_binary = shutil.which("agy")
    assert agy_binary is not None, (
        "agy must be installed on PATH: there is no binary to read the payload's fields from"
    )
    version = read_installed_agy_version()
    image = _load_go_image(Path(agy_binary))

    top_level_keys = sorted({json_path[0] for json_path in STATUSLINE_PAYLOAD_FIELD_PATHS})
    name_addresses = [
        address for json_key in top_level_keys for address in _find_field_name_addresses(image, json_key)
    ]
    struct_addresses = {
        _find_struct_holding_field(image, slot) for slot in _find_slots_pointing_at(image, name_addresses)
    }
    missing_paths_of_each_struct = [
        _find_missing_json_paths(image, struct_address)
        for struct_address in struct_addresses
        if struct_address is not None
    ]

    assert missing_paths_of_each_struct, (
        f"found no Go struct in {agy_binary} (agy {version}) with a field whose JSON key is any of "
        f"{top_level_keys}, the top-level statusLine payload fields statusline.sh reads: agy renamed them all, or "
        "this test no longer reads the binary's Go type descriptors"
    )
    closest_missing_paths = min(missing_paths_of_each_struct, key=len)
    assert not closest_missing_paths, (
        f"no Go struct in {agy_binary} (agy {version}) marshals every statusLine payload field statusline.sh reads; "
        f"the closest one lacks these paths, each given with the JSON keys the struct has there: "
        f"{closest_missing_paths}; agy renamed or dropped a field, so update statusline.sh and "
        "STATUSLINE_PAYLOAD_FIELD_PATHS to the payload agy now sends"
    )
