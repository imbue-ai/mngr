from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.registry import _backend_registry
from imbue.mngr.providers.registry import _indent_text
from imbue.mngr.providers.registry import get_all_provider_args_help_sections

_TEST_BACKEND_NAME = ProviderBackendName("test-same-help")


class _TestBackendWithSameHelp(ProviderBackendInterface):
    """Test backend where build and start args help are identical."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return _TEST_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Test backend"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        raise NotImplementedError


def test_indent_text_adds_prefix_to_each_line() -> None:
    result = _indent_text("line1\nline2\nline3", "  ")
    assert result == "  line1\n  line2\n  line3"


def test_indent_text_leaves_blank_lines_empty() -> None:
    result = _indent_text("line1\n\nline3", "  ")
    assert result == "  line1\n\n  line3"


def test_indent_text_handles_single_line() -> None:
    result = _indent_text("hello", ">>")
    assert result == ">>hello"


def test_indent_text_handles_whitespace_only_lines_as_blank() -> None:
    result = _indent_text("line1\n   \nline3", "  ")
    assert result == "  line1\n\n  line3"


def test_get_all_provider_args_help_sections_returns_single_section() -> None:
    sections = get_all_provider_args_help_sections()
    assert len(sections) == 1
    title, _content = sections[0]
    assert title == "Provider Build/Start Arguments"


def test_get_all_provider_args_help_sections_includes_all_registered_backends() -> None:
    sections = get_all_provider_args_help_sections()
    _title, content = sections[0]
    # The test fixture loads local and ssh backends
    assert "Provider: local" in content
    assert "Provider: ssh" in content


def test_get_all_provider_args_help_sections_includes_build_help_text() -> None:
    sections = get_all_provider_args_help_sections()
    _title, content = sections[0]
    # Local backend's build help should appear
    assert "No build arguments are supported for the local provider" in content


def test_get_all_provider_args_help_sections_includes_start_help_when_different_from_build() -> None:
    sections = get_all_provider_args_help_sections()
    _title, content = sections[0]
    # Local backend has different build and start help, so both should appear
    assert "No start arguments are supported for the local provider" in content
    # SSH backend also has different start help
    assert "No start arguments are supported for the SSH provider" in content


def test_get_all_provider_args_help_sections_omits_start_help_when_same_as_build() -> None:
    # Register a test backend with identical build and start help
    _backend_registry[_TEST_BACKEND_NAME] = _TestBackendWithSameHelp
    try:
        sections = get_all_provider_args_help_sections()
        _title, content = sections[0]
        # The test backend should be listed
        assert f"Provider: {_TEST_BACKEND_NAME}" in content
        # The help text should appear exactly once (not duplicated for start)
        test_backend_section_start = content.index(f"Provider: {_TEST_BACKEND_NAME}")
        # Find the next provider section (or end of content)
        remaining = content[test_backend_section_start:]
        next_provider_idx = remaining.find("\nProvider: ", 1)
        if next_provider_idx == -1:
            test_backend_section = remaining
        else:
            test_backend_section = remaining[:next_provider_idx]
        # "No arguments supported." should appear exactly once (build only, not duplicated for start)
        assert test_backend_section.count("No arguments supported.") == 1
    finally:
        del _backend_registry[_TEST_BACKEND_NAME]
