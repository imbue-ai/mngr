"""Release-marked verification that the transcript decoder's field map still matches agy.

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

ChatToolCall's fields (``_TOOL_CALL_NAME`` / ``_TOOL_CALL_ARGS``) are deliberately not checked:
it lives in ``codeium_common.proto``, which protobuf-go registers via the legacy gzip path and
the extractor does not recover (see the extractor's module docstring).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from imbue.mngr_antigravity.resources import decode_agy_transcript as dat

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
