"""Meta-test: every agent plugin that emits a common transcript has a conformance test.

The canonical envelope schema (:mod:`imbue.mngr.agents.common_transcript_records`) is only
worth anything if every emitter is actually validated against it. Each agent plugin that
emits a common transcript ships a ``test_emitted_common_records_conform_to_canonical_schema``
test that drives its real emitter and asserts every emitted record conforms. This meta-test
discovers those plugins from the registry and fails if any lacks that test -- so a new
agent plugin cannot merge without one, and the contract stays enforced rather than relying
on convention.

It checks *presence* (by reading the plugin package's test sources), not execution: the
conformance tests themselves run the heterogeneous emitters (TypeScript via node, shell via
bash) and skip where the toolchain is absent, but the requirement that they *exist* is
toolchain-independent and always enforced here.
"""

import inspect
from pathlib import Path

import pluggy

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.config.agent_class_registry import get_agent_class
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin

_CONFORMANCE_TEST_NAME = "test_emitted_common_records_conform_to_canonical_schema"

# Safety net: these emitter plugins must be discovered, so a misconfigured registry that
# loaded nothing can't make this meta-test vacuously pass. (Subtypes like ``code-guardian``
# share their package's emitter/test, so only the distinct emitter packages are listed.)
_EXPECTED_EMITTER_TYPES = frozenset({"claude", "antigravity", "opencode", "pi-coding", "codex"})


def _emits_common_transcript(agent_type: str) -> bool:
    agent_class = get_agent_class(agent_type)
    return (
        isinstance(agent_class, type)
        and issubclass(agent_class, HasCommonTranscriptMixin)
        # Only first-party agent-plugin packages (``imbue.mngr_<x>``). Excludes core built-ins
        # (``command``/``headless_command``) and the registered test placeholder, which live
        # under ``imbue.mngr.`` and do not emit a common transcript.
        and agent_class.__module__.startswith("imbue.mngr_")
    )


def _package_has_conformance_test(agent_type: str) -> bool:
    package_dir = Path(inspect.getfile(get_agent_class(agent_type))).parent
    return any(_CONFORMANCE_TEST_NAME in test_file.read_text() for test_file in package_dir.rglob("*_test.py"))


def test_every_common_transcript_plugin_has_a_conformance_test(plugin_manager: pluggy.PluginManager) -> None:
    emitter_types = {name for name in list_registered_agent_types() if _emits_common_transcript(name)}

    assert _EXPECTED_EMITTER_TYPES <= emitter_types, (
        "expected common-transcript emitter plugins are not all registered (registry loaded a "
        f"subset?): missing {sorted(_EXPECTED_EMITTER_TYPES - emitter_types)}"
    )

    missing = sorted(name for name in emitter_types if not _package_has_conformance_test(name))
    assert not missing, (
        f"these agent plugins emit a common transcript but ship no `{_CONFORMANCE_TEST_NAME}` "
        "test. Add one that drives the real emitter and validates each emitted record with "
        "imbue.mngr.agents.common_transcript_records.validate_common_transcript_record "
        f"(see the existing ones for the pattern): {missing}"
    )
