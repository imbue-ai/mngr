"""Checking ``@pytest.mark.witnesses`` markers against the spec corpus.

Tests back-link to spec units with ``@pytest.mark.witnesses("<coordinate>",
partial=...)`` (see the minds-behavioral-specs skill). This module finds
those markers in Python sources (as decorators and in ``pytestmark``
assignments) and checks every referenced coordinate against the units of a
scanned corpus: a marker naming a coordinate no unit claims is a stale or
mistyped link, and ``minds specs check-witnesses`` fails on it.
"""

import ast
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.core.behavioral_specs.data_types import WitnessMarker
from imbue.minds.core.behavioral_specs.data_types import WitnessProblem


class WitnessScan(FrozenModel):
    """The witnesses markers found in a set of Python files, plus any problems reading them."""

    markers: tuple[WitnessMarker, ...] = Field(description="All witnesses markers found, in file order")
    problems: tuple[WitnessProblem, ...] = Field(
        description="Files that could not be parsed and markers with non-literal coordinates"
    )


@pure
def _is_witnesses_call(node: ast.AST) -> bool:
    """True for calls of exactly the form ``pytest.mark.witnesses(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "witnesses"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "mark"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "pytest"
    )


@pure
def _marker_from_call(call: ast.Call, file: Path) -> WitnessMarker | WitnessProblem:
    """Build a WitnessMarker from a witnesses call, or a problem if the coordinate is not literal."""
    coordinate: str | None = None
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        coordinate = call.args[0].value
    if coordinate is None:
        return WitnessProblem(
            file=file,
            line=call.lineno,
            message=(
                "witnesses marker's first argument must be a coordinate string literal "
                "(computed coordinates cannot be checked against the corpus)"
            ),
        )
    partial: str | None = None
    for keyword in call.keywords:
        if keyword.arg == "partial" and isinstance(keyword.value, ast.Constant) and isinstance(
            keyword.value.value, str
        ):
            partial = keyword.value.value
    return WitnessMarker(coordinate=coordinate, file=file, line=call.lineno, partial=partial)


@pure
def _pytestmark_calls(value: ast.AST) -> tuple[ast.Call, ...]:
    """Extract the call nodes of a pytestmark value: a single call or a list/tuple of calls."""
    if isinstance(value, ast.Call):
        return (value,)
    if isinstance(value, (ast.List, ast.Tuple)):
        return tuple(element for element in value.elts if isinstance(element, ast.Call))
    return ()


def find_witness_markers_in_source(source_text: str, file: Path) -> WitnessScan:
    """Find every pytest.mark.witnesses application in one Python source.

    Raises SyntaxError if the source cannot be parsed (callers decide whether
    that is a problem to report).
    """
    markers: list[WitnessMarker] = []
    problems: list[WitnessProblem] = []
    tree = ast.parse(source_text, filename=str(file))
    for node in ast.walk(tree):
        calls: tuple[ast.Call, ...] = ()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            calls = tuple(
                decorator for decorator in node.decorator_list if _is_witnesses_call(decorator)
            )
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            calls = tuple(call for call in _pytestmark_calls(node.value) if _is_witnesses_call(call))
        for call in calls:
            result = _marker_from_call(call, file)
            if isinstance(result, WitnessMarker):
                markers.append(result)
            else:
                problems.append(result)
    return WitnessScan(markers=tuple(markers), problems=tuple(problems))


def _iter_python_files(root: Path) -> list[Path]:
    """Yield every .py file under root, skipping hidden entries and __pycache__, sorted."""
    python_files: list[Path] = []
    for folder, child_folder_names, file_names in root.walk(top_down=True):
        child_folder_names[:] = sorted(
            name for name in child_folder_names if not name.startswith(".") and name != "__pycache__"
        )
        for file_name in sorted(file_names):
            if file_name.endswith(".py") and not file_name.startswith("."):
                python_files.append(folder / file_name)
    return python_files


def find_witness_markers_in_paths(paths: Sequence[Path]) -> WitnessScan:
    """Find every witnesses marker under the given files/directories.

    A path that is a file is scanned directly; a directory is walked
    recursively. Unparseable files are reported as problems.
    """
    markers: list[WitnessMarker] = []
    problems: list[WitnessProblem] = []
    for path in paths:
        python_files = [path] if path.is_file() else _iter_python_files(path)
        for python_file in python_files:
            try:
                scan = find_witness_markers_in_source(python_file.read_text(encoding="utf-8"), python_file)
            except SyntaxError:
                problems.append(
                    WitnessProblem(file=python_file, line=None, message="file could not be parsed as Python")
                )
                continue
            markers.extend(scan.markers)
            problems.extend(scan.problems)
    return WitnessScan(markers=tuple(markers), problems=tuple(problems))


@pure
def check_witness_markers(
    scan: WitnessScan,
    valid_coordinates: frozenset[str],
) -> tuple[WitnessProblem, ...]:
    """Report every marker whose coordinate no corpus unit claims, after the scan's own problems."""
    unknown = tuple(
        WitnessProblem(
            file=marker.file,
            line=marker.line,
            message=f"unknown coordinate '{marker.coordinate}': no unit in the spec corpus claims it",
        )
        for marker in scan.markers
        if marker.coordinate not in valid_coordinates
    )
    return scan.problems + unknown
