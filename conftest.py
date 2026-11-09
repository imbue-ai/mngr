"""Root conftest for the monorepo.

Common pytest hooks (test locking, timing limits, output file redirection) are
provided by the shared module imbue.imbue_common.conftest_hooks. Each project's
conftest.py calls register_conftest_hooks(globals()) to inject them. The shared
module ensures hooks are only registered once even when multiple conftest.py files
are discovered (e.g., when running from the monorepo root).

Resource guards are discovered via the resource_guards entry point group;
no manual guard registration is needed here. See the docstring of
libs/mngr/imbue/mngr/register_guards.py for how guards are wired up in this
monorepo and how to add new ones.
"""

import ast
from pathlib import Path

import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.imbue_common.ratchet_testing.core import _get_all_text_files_with_extension
from imbue.mngr.utils.logging import suppress_warnings

suppress_warnings()

register_conftest_hooks(globals())

_REPO_ROOT = Path(__file__).parent


@pytest.fixture(scope="session")
def model_config_class_defs() -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Each class's bare base names, and where its body sets extra="forbid", across every Python file in the repo.

    Returns ``(base_names_by_class, forbid_locations_by_class)``. Classes are keyed
    by bare name; two same-named classes in different files have their bases merged,
    which can only over-approximate a base's subclass set (acceptable for guards
    that should match nothing). Session-scoped so the repo-wide parse runs once per
    process, during setup, outside its consumers' timeouts.
    """
    base_names_by_class: dict[str, set[str]] = {}
    forbid_locations_by_class: dict[str, list[str]] = {}
    for py_path in _get_all_text_files_with_extension(_REPO_ROOT, ".py"):
        try:
            tree = ast.parse(py_path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.id if isinstance(base, ast.Name) else base.attr
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            }
            base_names_by_class.setdefault(node.name, set()).update(base_names)
            if _class_body_sets_extra_forbid(node):
                forbid_locations_by_class.setdefault(node.name, []).append(
                    f"{py_path.relative_to(_REPO_ROOT)}:{node.lineno}"
                )
    return base_names_by_class, forbid_locations_by_class


def _class_body_sets_extra_forbid(class_def: ast.ClassDef) -> bool:
    """Whether the class body assigns a model_config containing extra="forbid".

    Handles all three spellings: ``model_config = ConfigDict(extra="forbid")``,
    the plain-dict form ``model_config = {"extra": "forbid"}``, and the annotated
    form ``model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")``.
    """
    for statement in class_def.body:
        if isinstance(statement, ast.Assign):
            if not any(isinstance(t, ast.Name) and t.id == "model_config" for t in statement.targets):
                continue
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            if not (isinstance(statement.target, ast.Name) and statement.target.id == "model_config"):
                continue
            if statement.value is None:
                continue
            value = statement.value
        else:
            continue
        if _config_value_sets_extra_forbid(value):
            return True
    return False


def _config_value_sets_extra_forbid(value: ast.expr) -> bool:
    """Whether a model_config value expression contains extra="forbid"."""
    if isinstance(value, ast.Call):
        for keyword in value.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value == "forbid":
                    return True
    elif isinstance(value, ast.Dict):
        for key, dict_value in zip(value.keys, value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "extra":
                if isinstance(dict_value, ast.Constant) and dict_value.value == "forbid":
                    return True
    else:
        pass
    return False


# apps/minds_evals is a standalone uv project, not a workspace member (see the
# root pyproject's [tool.uv.workspace].exclude), so neither its package nor
# harbor exists in this venv: collecting its tests raises ModuleNotFoundError,
# which is a hard collection error that aborts the entire run (including
# offload's --collect-only discovery phase). Its suite runs under its own
# project via `just test-minds-evals`.
#
# Two things about the form. It lives here rather than in addopts because CI
# invocations that pass --override-ini='addopts=...' (see
# .github/workflows/release-tests.yml) discard addopts entirely. And it must be
# the glob variant matching *files*: pytest's ignore hook compares plain
# collect_ignore entries by exact path equality against each candidate, and
# `testpaths = ["apps/*", ...]` hands pytest the directory as an explicit
# initial argument, so a bare directory entry never matches anything.
collect_ignore_glob = ["apps/minds_evals/*"]
