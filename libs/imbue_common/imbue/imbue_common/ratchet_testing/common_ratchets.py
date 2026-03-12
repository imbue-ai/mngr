from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RatchetMatchChunk
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import format_ratchet_failure_message
from imbue.imbue_common.ratchet_testing.core import get_ratchet_failures


class RatchetRuleInfo(FrozenModel):
    """Metadata for a ratchet rule (name and description for failure messages)."""

    rule_name: str = Field(description="Name of the ratchet rule used in failure messages")
    rule_description: str = Field(description="Explanation of the rule shown in failure messages")

    def format_failure(self, chunks: tuple[RatchetMatchChunk, ...]) -> str:
        """Format a failure message for this ratchet rule violation."""
        return format_ratchet_failure_message(self.rule_name, self.rule_description, chunks)


class RegexRatchetRule(RatchetRuleInfo):
    """A reusable regex-based ratchet rule definition with pattern, name, and description."""

    pattern_string: str = Field(description="The regex pattern string to search for")
    is_multiline: bool = Field(default=False, description="Whether to compile the pattern with re.MULTILINE")


def check_ratchet_rule(
    rule: RegexRatchetRule,
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Run a regex-based ratchet rule against Python files in a source directory."""
    pattern = RegexPattern(rule.pattern_string, multiline=rule.is_multiline)
    return check_regex_ratchet(source_dir, FileExtension(".py"), pattern, excluded_path_patterns)


def check_ratchet_rule_all_files(
    rule: RegexRatchetRule,
    source_dir: Path,
    excluded_path_patterns: tuple[str, ...] = (),
) -> tuple[RatchetMatchChunk, ...]:
    """Run a regex-based ratchet rule against all tracked files (not just .py)."""
    pattern = RegexPattern(rule.pattern_string, multiline=rule.is_multiline)
    return get_ratchet_failures(source_dir, None, pattern, excluded_path_patterns)


# --- Code safety ---

PREVENT_TODOS = RegexRatchetRule(
    rule_name="TODO comments",
    rule_description="TODO comments should not increase (ideally should decrease to zero)",
    pattern_string=r"# TODO:.*",
)

PREVENT_EXEC = RegexRatchetRule(
    rule_name="exec() usages",
    rule_description="exec() should not be used due to security and maintainability concerns",
    pattern_string=r"(?<!\.)\bexec\s*\(",
)

PREVENT_EVAL = RegexRatchetRule(
    rule_name="eval() usages",
    rule_description="eval() should not be used due to security and maintainability concerns",
    pattern_string=r"\beval\s*\(",
)

PREVENT_WHILE_TRUE = RegexRatchetRule(
    rule_name="while True loops",
    rule_description="'while True' loops can cause infinite loops and make code harder to reason about. Use explicit conditions instead",
    pattern_string=r"\bwhile\s+True\s*:",
)

PREVENT_TIME_SLEEP = RegexRatchetRule(
    rule_name="time.sleep usage",
    rule_description="time.sleep is an antipattern. Instead, poll for the condition that you expect to be true. See wait_for",
    pattern_string=r"\btime\.sleep\s*\(|\bfrom\s+time\s+import\s+sleep\b",
)

PREVENT_GLOBAL_KEYWORD = RegexRatchetRule(
    rule_name="global keyword usage",
    rule_description="Avoid using the 'global' keyword. Pass state explicitly through function parameters instead",
    pattern_string=r"^\s*global\s+\w+",
)

PREVENT_BARE_PRINT = RegexRatchetRule(
    rule_name="bare print statements",
    rule_description=(
        "Do not use bare print statements. Consider what kind of output you are producing: "
        "for user-facing command output (results, tables, status messages), use write_human_line(); "
        "for diagnostic/debug messages, use logger.info(), logger.debug(), logger.warning(), etc."
    ),
    pattern_string=r"^\s*print\s*\(",
    is_multiline=True,
)

PREVENT_GETATTR = RegexRatchetRule(
    rule_name="getattr() usages",
    rule_description="getattr() bypasses the type system and makes code harder to reason about. Access attributes directly instead",
    pattern_string=r"(?<!\.)\bgetattr\s*\(",
)

PREVENT_SETATTR = RegexRatchetRule(
    rule_name="setattr() usages",
    rule_description="setattr() bypasses the type system and makes code harder to reason about. Set attributes directly instead",
    pattern_string=r"(?<!\.)\bsetattr\s*\(",
)


# --- Exception handling ---

PREVENT_BARE_EXCEPT = RegexRatchetRule(
    rule_name="bare except clauses",
    rule_description="Bare 'except:' catches all exceptions including system exits. Use specific exception types instead",
    pattern_string=r"except\s*:",
)

PREVENT_BROAD_EXCEPTION_CATCH = RegexRatchetRule(
    rule_name="except Exception catches",
    rule_description="Catching 'Exception' is too broad. Use specific exception types instead",
    pattern_string=r"except\s+Exception\b",
)

PREVENT_BASE_EXCEPTION_CATCH = RegexRatchetRule(
    rule_name="except BaseException catches",
    rule_description="Catching 'BaseException' catches system exits and keyboard interrupts. Use specific exception types instead",
    pattern_string=r"except\s+BaseException\b",
)

PREVENT_BUILTIN_EXCEPTION_RAISES = RegexRatchetRule(
    rule_name="direct raising of built-in exceptions",
    rule_description="Never raise built-in exceptions directly. Create custom exception types that inherit from both the package base exception and the built-in",
    pattern_string=r"raise\s+(ValueError|KeyError|TypeError|AttributeError|IndexError|RuntimeError|OSError|IOError|KeyboardInterrupt)\(\b",
)


# --- Import style ---

PREVENT_INLINE_IMPORTS = RegexRatchetRule(
    rule_name="inline imports",
    rule_description="Imports should be at the top of the file, not inline within functions",
    pattern_string=r"^[ \t]+import\s+\w+|^[ \t]+from\s+\S+\s+import\b",
    is_multiline=True,
)

PREVENT_RELATIVE_IMPORTS = RegexRatchetRule(
    rule_name="relative imports",
    rule_description="Always use absolute imports, never relative imports. Use 'from imbue.module' instead of 'from .'",
    pattern_string=r"^from\s+\.",
    is_multiline=True,
)

PREVENT_IMPORT_DATETIME = RegexRatchetRule(
    rule_name="import datetime",
    rule_description="Do not use 'import datetime'. Import specific items instead: 'from datetime import datetime, timedelta, etc.'",
    pattern_string=r"^import datetime$",
    is_multiline=True,
)


# --- Banned libraries and patterns ---

PREVENT_ASYNCIO_IMPORT = RegexRatchetRule(
    rule_name="asyncio imports",
    rule_description="asyncio is banned per style guide. Use synchronous code instead",
    pattern_string=r"\bimport\s+asyncio\b|\bfrom\s+asyncio\b",
)

PREVENT_PANDAS_IMPORT = RegexRatchetRule(
    rule_name="pandas imports",
    rule_description="pandas is banned per style guide. Use polars instead",
    pattern_string=r"\bimport\s+pandas\b|\bfrom\s+pandas\b",
)

PREVENT_DATACLASSES_IMPORT = RegexRatchetRule(
    rule_name="dataclasses imports",
    rule_description="dataclasses are banned per style guide. Use pydantic models instead",
    pattern_string=r"\bimport\s+dataclasses\b|\bfrom\s+dataclasses\b",
)

PREVENT_NAMEDTUPLE = RegexRatchetRule(
    rule_name="namedtuple usage",
    rule_description="namedtuple is banned per style guide. Use pydantic models instead",
    pattern_string=r"\bnamedtuple\s*\(|\bNamedTuple\b",
)

PREVENT_YAML_USAGE = RegexRatchetRule(
    rule_name="yaml usage",
    rule_description="NEVER use YAML files. Use TOML for configuration instead",
    pattern_string=r"yaml",
    is_multiline=True,
)

PREVENT_FUNCTOOLS_PARTIAL = RegexRatchetRule(
    rule_name="functools.partial usage",
    rule_description="functools.partial is banned. It leads to confusing code and makes debugging difficult. Use explicit wrapper functions or lambdas instead",
    pattern_string=r"\bfrom\s+functools\s+import\s+.*\bpartial\b|\bfunctools\.partial\b",
)


# --- Naming conventions ---

PREVENT_NUM_PREFIX = RegexRatchetRule(
    rule_name="num prefix usage",
    rule_description="Avoid using 'num' prefix. Use 'count' or 'idx' instead (e.g., 'user_count' not 'num_users')",
    pattern_string=r"\bnum_\w+",
)


# --- Documentation ---

PREVENT_TRAILING_COMMENTS = RegexRatchetRule(
    rule_name="trailing comments",
    rule_description="Comments should be on their own line, not trailing after code. Trailing comments make code harder to read",
    pattern_string=r"[^\s#].*[ \t]#(?!\s*ty:\s*ignore\[)",
)

PREVENT_INIT_DOCSTRINGS = RegexRatchetRule(
    rule_name="docstrings in __init__ methods",
    rule_description="Never create docstrings for __init__ methods. The class docstring should describe the class, not __init__",
    pattern_string=r'def __init__[^:]*:\s+"""',
    is_multiline=True,
)

PREVENT_ARGS_IN_DOCSTRINGS = RegexRatchetRule(
    rule_name="Args: sections in docstrings",
    rule_description="Never include 'Args:' sections in docstrings. Use inline parameter comments if needed",
    pattern_string=r'"""[\s\S]{0,500}Args:',
    is_multiline=True,
)

PREVENT_RETURNS_IN_DOCSTRINGS = RegexRatchetRule(
    rule_name="Returns: sections in docstrings",
    rule_description="Never include 'Returns:' sections in docstrings. Use inline return type comments if needed",
    pattern_string=r'"""[\s\S]{0,500}Returns:',
    is_multiline=True,
)


# --- Type safety ---

PREVENT_LITERAL_MULTIPLE_OPTIONS = RegexRatchetRule(
    rule_name="Literal with multiple options",
    rule_description="Never use Literal with multiple string options. Create an UpperCaseStrEnum instead per the style guide",
    pattern_string=r"Literal\[.*,.*\]",
)

PREVENT_BARE_GENERIC_TYPES = RegexRatchetRule(
    rule_name="bare generic types",
    rule_description="Generic types must specify their type parameters. Use 'list[str]' not 'list', 'dict[str, int]' not 'dict', etc.",
    pattern_string=r":\s*(list|dict|tuple|set|List|Dict|Tuple|Set|Mapping|Sequence)\s*($|[,\)\]])",
)

PREVENT_TYPING_BUILTIN_IMPORTS = RegexRatchetRule(
    rule_name="typing module imports for builtin types",
    rule_description="Do not import Dict, List, Set, or Tuple from typing. Use lowercase builtin types (dict, list, set, tuple) instead",
    pattern_string=r"\bfrom\s+typing\s+import\s+.*\b(Dict|List|Set|Tuple)\b",
)

PREVENT_SHORT_UUID_IDS = RegexRatchetRule(
    rule_name="short uuid4 IDs",
    rule_description=(
        "Do not truncate uuid4() to create short IDs (e.g., uuid4().hex[:8]). "
        "Use the full uuid4().hex instead to ensure uniqueness, or use get_short_random_string() in tests if necessary"
    ),
    pattern_string=r"uuid4\(\)(\.hex)?\[",
)


# --- Pydantic / models ---

PREVENT_MODEL_COPY = RegexRatchetRule(
    rule_name=".model_copy() usage",
    rule_description=(
        "Do not use .model_copy() directly. "
        "Use model_copy_update instead: obj.model_copy_update(to_update(obj.field_ref().field, value)). "
        "See style guide 'Type-safe model_copy_update' section."
    ),
    pattern_string=r"\.model_copy\(",
)


# --- Logging ---

PREVENT_FSTRING_LOGGING = RegexRatchetRule(
    rule_name="f-string logging",
    rule_description=(
        "Do not use f-strings with loguru. Use loguru-style placeholder syntax instead: "
        "logger.info('message {}', var) instead of logger.info(f'message {var}')"
    ),
    pattern_string=r"logger\.(trace|debug|info|warning|error|exception)\(f",
)

PREVENT_CLICK_ECHO = RegexRatchetRule(
    rule_name="click.echo usage",
    rule_description=(
        "Do not use click.echo. For user-facing command output, use write_human_line(); "
        "for diagnostic/debug messages, use logger.info(), logger.debug(), etc."
    ),
    pattern_string=r"\bclick\.echo\b|\bfrom\s+click\s+import\s+.*\becho\b",
)


# --- Testing conventions ---

PREVENT_UNITTEST_MOCK_IMPORTS = RegexRatchetRule(
    rule_name="unittest.mock imports",
    rule_description=(
        "Do not import from unittest.mock. Mock, MagicMock, patch, create_autospec, etc. make tests "
        "brittle and disconnected from real behavior. Instead, create concrete mock implementations "
        "of interfaces in mock_*_test.py files. See the style guide Testing section for details."
    ),
    pattern_string=r"from unittest\.mock import|from unittest import mock",
)

PREVENT_MONKEYPATCH_SETATTR = RegexRatchetRule(
    rule_name="monkeypatch.setattr usages",
    rule_description=(
        "Do not use monkeypatch.setattr to replace attributes or functions at runtime. "
        "Use dependency injection and concrete mock implementations of interfaces instead. "
        "Note: monkeypatch.setenv, monkeypatch.delenv, and monkeypatch.chdir are fine."
    ),
    pattern_string=r"monkeypatch\.setattr",
)

PREVENT_TEST_CONTAINER_CLASSES = RegexRatchetRule(
    rule_name="test container classes",
    rule_description=(
        "Do not use Test* or *Test classes in test files. Tests should be top-level functions, "
        "not methods inside container classes. If a class is used as a test fixture (not a test "
        "container), rename it so pytest does not collect it (e.g., SamplePlugin instead of TestPlugin)"
    ),
    pattern_string=r"^class\s+(Test\w*|\w+Test)\s*[\(:]",
    is_multiline=True,
)

PREVENT_PYTEST_MARK_INTEGRATION = RegexRatchetRule(
    rule_name="pytest.mark.integration usage",
    rule_description=(
        "Do not use @pytest.mark.integration. Integration tests should go in files whose name "
        "starts with 'test_' without any marker. See the style guide for the correct test types"
    ),
    pattern_string=r"pytest\.mark\.integration",
)


# --- AST-based ratchet metadata ---

PREVENT_IF_ELIF_WITHOUT_ELSE = RatchetRuleInfo(
    rule_name="if/elif without else",
    rule_description="All if/elif chains must have an else clause to ensure all cases are handled explicitly",
)

PREVENT_INIT_IN_NON_EXCEPTION_CLASSES = RatchetRuleInfo(
    rule_name="__init__ methods in non-Exception/Error classes",
    rule_description=(
        "Do not define __init__ methods in non-Exception/Error classes. "
        "Use Pydantic models instead, which handle initialization automatically"
    ),
)

PREVENT_INLINE_FUNCTIONS = RatchetRuleInfo(
    rule_name="inline functions in non-test code",
    rule_description=(
        "Functions should not be defined inside other functions in non-test code. "
        "Extract them as top-level functions or methods"
    ),
)

PREVENT_UNDERSCORE_IMPORTS = RatchetRuleInfo(
    rule_name="importing underscore-prefixed names in non-test code",
    rule_description=(
        "Do not import underscore-prefixed functions/classes/constants in non-test code. "
        "These are private and should not be used outside their defining module"
    ),
)

PREVENT_CAST_USAGE = RatchetRuleInfo(
    rule_name="cast() usages",
    rule_description=(
        "Do not use cast() from typing. It bypasses the type checker and makes code less safe. "
        "If you need to override the type checker, use a '# ty: ignore[specific-error]' comment instead, "
        "but only if there is really no other way. Consider restructuring your code to avoid the need for type overrides."
    ),
)

PREVENT_ASSERT_ISINSTANCE = RatchetRuleInfo(
    rule_name="assert isinstance() usages",
    rule_description=(
        "Do not use 'assert isinstance()'. Use match statements with exhaustive case handling instead. "
        "End your match with 'case _ as unreachable: assert_never(unreachable)' to ensure all cases are "
        "handled and catch new variants at compile time. See style guide for examples."
    ),
)


# --- Process management ---

PREVENT_DIRECT_SUBPROCESS = RegexRatchetRule(
    rule_name="direct subprocess/os.exec usage",
    rule_description=(
        "Do not use subprocess.Popen, subprocess.run, subprocess.call, subprocess.check_call, "
        "subprocess.check_output, os.exec*, os.spawn*, os.system, or os.popen directly. "
        "Instead, use run_process_to_completion from ConcurrencyGroup and ensure a ConcurrencyGroup "
        "is passed down to the call site. This ensures all spawned processes get cleaned up properly. "
        "See libs/concurrency_group/ for details."
    ),
    pattern_string=(
        r"\bfrom\s+subprocess\s+import\b(Popen|run|call|check_call|check_output|getoutput|getstatusoutput)"
        r"|\bsubprocess\.(Popen|run|call|check_call|check_output|getoutput|getstatusoutput)\b"
        r"|\bos\.(exec\w+|spawn\w+|system|popen)\b"
        r"|\bfrom\s+os\s+import\b.*\b(exec\w+|spawn\w+|system|popen)\b"
    ),
)

PREVENT_OS_FORK = RegexRatchetRule(
    rule_name="os.fork usage",
    rule_description=(
        "Do not use os.fork or os.forkpty. Forking is incompatible with threading: a forked child "
        "inherits only the calling thread, leaving mutexes held by other threads permanently locked "
        "and shared state inconsistent. Use the subprocess module to launch subprocesses instead. "
        "The remaining uses of os.fork will be removed from the codebase entirely."
    ),
    pattern_string=(
        r"\bos\.fork\w*\b"
        r"|\bfrom\s+os\s+import\b.*\bfork\w*\b"
    ),
)

PREVENT_IMPORTLIB_IMPORT_MODULE = RegexRatchetRule(
    rule_name="importlib.import_module usage",
    rule_description="Always use normal top-level imports instead of importlib.import_module",
    pattern_string=r"\bimport_module\b",
)
